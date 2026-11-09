#include "BoundedFrameWriter.h"

#include <algorithm>
#include <limits>
#include <stdexcept>
#include <utility>

namespace capture
{

BoundedFrameWriter::BoundedFrameWriter(CompletionObserver write_frame, ErrorObserver on_error, WriterLimits limits)
    : limits_(limits)
    , write_frame_(std::move(write_frame))
    , on_error_(std::move(on_error))
{
    if (!write_frame_ || limits_.max_frames == 0 || limits_.max_bytes == 0) {
        throw std::invalid_argument("A frame consumer and positive writer limits are required");
    }
    thread_ = std::thread([this] { run(); });
}

BoundedFrameWriter::~BoundedFrameWriter()
{
    stop();
}

SubmitResult BoundedFrameWriter::reject(const FrameCompletion& completion, SubmitResult result)
{
    ++stats_.dropped;
    stats_.last_dropped_frame_id = completion.frame_id;
    switch (result) {
    case SubmitResult::Full:
        stats_.last_drop_reason = "full";
        break;
    case SubmitResult::Oversized:
        stats_.last_drop_reason = "oversized";
        break;
    case SubmitResult::UnsupportedImage:
        stats_.last_drop_reason = "unsupported_image";
        break;
    case SubmitResult::Stopped:
        stats_.last_drop_reason = "stopped";
        break;
    case SubmitResult::CloneFailed:
        stats_.last_drop_reason = "clone_failed";
        break;
    case SubmitResult::Accepted:
        break;
    }
    return result;
}

SubmitResult BoundedFrameWriter::submit(const FrameCompletion& completion, const cv::Mat& image)
{
    std::size_t bytes = 0;
    {
        std::lock_guard lock(mutex_);
        if (stopping_) {
            return reject(completion, SubmitResult::Stopped);
        }
        if (!image.empty()) {
            if (image.dims != 2 || image.depth() != CV_8U || (image.channels() != 1 && image.channels() != 3 && image.channels() != 4)) {
                return reject(completion, SubmitResult::UnsupportedImage);
            }
            if (image.total() > std::numeric_limits<std::size_t>::max() / image.elemSize()) {
                return reject(completion, SubmitResult::Oversized);
            }
            bytes = image.total() * image.elemSize();
        }
        if (bytes > limits_.max_bytes) {
            return reject(completion, SubmitResult::Oversized);
        }
        if (stats_.reserved_frames >= limits_.max_frames || bytes > limits_.max_bytes - stats_.reserved_bytes) {
            return reject(completion, SubmitResult::Full);
        }
        ++stats_.reserved_frames;
        stats_.reserved_bytes += bytes;
        stats_.peak_frames = std::max(stats_.peak_frames, stats_.reserved_frames);
        stats_.peak_bytes = std::max(stats_.peak_bytes, stats_.reserved_bytes);
        ++cloning_;
    }
    PendingFrame pending;
    try {
        pending = { completion, image.clone(), bytes };
    }
    catch (...) {
        std::lock_guard lock(mutex_);
        --cloning_;
        release(bytes);
        ready_.notify_one();
        return reject(completion, SubmitResult::CloneFailed);
    }
    std::lock_guard lock(mutex_);
    --cloning_;
    if (stopping_ && !drain_) {
        pending.image.release();
        release(bytes);
        ready_.notify_one();
        return reject(completion, SubmitResult::Stopped);
    }
    try {
        queue_.push_back(std::move(pending));
    }
    catch (...) {
        pending.image.release();
        release(bytes);
        ready_.notify_one();
        return reject(completion, SubmitResult::CloneFailed);
    }
    ++stats_.accepted;
    ready_.notify_one();
    return SubmitResult::Accepted;
}

WriterStats BoundedFrameWriter::stats() const
{
    std::lock_guard lock(mutex_);
    return stats_;
}

void BoundedFrameWriter::release(std::size_t bytes)
{
    --stats_.reserved_frames;
    stats_.reserved_bytes -= bytes;
}

void BoundedFrameWriter::discardQueued()
{
    for (auto& frame : queue_) {
        // Keep reservations until the corresponding owned image has actually been released.
        const auto bytes = frame.bytes;
        frame.image.release();
        release(bytes);
        ++stats_.aborted;
    }
    queue_.clear();
}

void BoundedFrameWriter::run()
{
    for (;;) {
        PendingFrame frame;
        {
            std::unique_lock lock(mutex_);
            ready_.wait(lock, [this] { return !queue_.empty() || (stopping_ && cloning_ == 0); });
            if (queue_.empty()) {
                return;
            }
            frame = std::move(queue_.front());
            queue_.pop_front();
        }
        std::string error;
        try {
            write_frame_(frame.completion, frame.image);
        }
        catch (const std::exception& exception) {
            error = exception.what();
            if (error.empty()) {
                error = "frame writer exception without a message";
            }
        }
        catch (...) {
            error = "unknown frame writer exception";
        }
        frame.image.release();
        {
            std::lock_guard lock(mutex_);
            release(frame.bytes);
            if (error.empty()) {
                ++stats_.written;
            }
            else {
                ++stats_.aborted;
                stats_.error = error;
                stopping_ = true;
                drain_ = false;
                discardQueued();
            }
        }
        if (!error.empty() && on_error_) {
            try {
                on_error_(error);
            }
            catch (...) {
                // Error observers report to the coordinator and cannot terminate the writer thread.
            }
        }
    }
}

void BoundedFrameWriter::stop(bool drain)
{
    {
        std::lock_guard lock(mutex_);
        stopping_ = true;
        drain_ = drain_ && drain;
        if (!drain_) {
            discardQueued();
        }
    }
    ready_.notify_one();
    if (thread_.joinable() && thread_.get_id() != std::this_thread::get_id()) {
        thread_.join();
    }
}

} // namespace capture
