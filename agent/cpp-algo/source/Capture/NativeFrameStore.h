#pragma once

#include <cstdio>
#include <filesystem>
#include <memory>

#include "BoundedFrameWriter.h"

namespace capture
{

class NativeFrameStore
{
public:
    NativeFrameStore(const std::filesystem::path& workspace, CaptureClock clock);
    void write(const FrameCompletion& completion, const cv::Mat& image);
    void finish(const WriterStats& stats);
    const std::filesystem::path& runPath() const;

private:
    CaptureClock clock_;
    std::filesystem::path run_path_;
    std::unique_ptr<std::FILE, decltype(&std::fclose)> frames_ { nullptr, &std::fclose };
    std::uint64_t last_sequence_ = 0;
    bool finished_ = false;
};

} // namespace capture
