#include "PassiveRecorder.h"

#include <cstdlib>
#include <stdexcept>

#include <MaaUtils/Logger.h>
#include <MaaUtils/Platform.h>

#include "../MapNavigator/sensing/position_provider.h"

namespace capture
{

PassiveRecorder::PassiveRecorder(mapnavigator::PositionProvider& provider, const std::filesystem::path& workspace)
    : store_(workspace, provider.captureClock())
    , writer_(
          [this](const FrameCompletion& completion, const cv::Mat& image) { store_.write(completion, image); },
          [this](const std::string&) { failed_.store(true); })
    , subscription_(provider.subscribeCompletion(
          [this](const FrameCompletion& completion, const cv::Mat& image) { writer_.submit(completion, image); }))
{
}

PassiveRecorder::~PassiveRecorder()
{
    stop();
}

bool PassiveRecorder::failed() const
{
    return failed_.load();
}

bool PassiveRecorder::stop()
{
    if (stopped_) {
        return !failed();
    }
    subscription_.reset();
    writer_.stop();
    try {
        store_.finish(writer_.stats());
    }
    catch (const std::exception& error) {
        failed_.store(true);
        LogError << "Native capture summary could not be finalized." << error.what();
    }
    stopped_ = true;
    return !failed();
}

WriterStats PassiveRecorder::stats() const
{
    return writer_.stats();
}

const std::filesystem::path& PassiveRecorder::runPath() const
{
    return store_.runPath();
}

std::unique_ptr<PassiveRecorder> AttachPassiveRecorder(mapnavigator::PositionProvider& provider)
{
#ifdef _WIN32
    wchar_t* buffer = nullptr;
    std::size_t length = 0;
    if (_wdupenv_s(&buffer, &length, L"MAAEND_NATIVE_CAPTURE_WORKSPACE") != 0) {
        throw std::runtime_error("Cannot read MAAEND_NATIVE_CAPTURE_WORKSPACE");
    }
    const std::unique_ptr<wchar_t, decltype(&std::free)> value(buffer, &std::free);
    if (!buffer || !*buffer) {
        return { };
    }
    const std::filesystem::path workspace(buffer);
#else
    const auto* value = std::getenv("MAAEND_NATIVE_CAPTURE_WORKSPACE");
    if (!value || !*value) {
        return { };
    }
    const auto workspace = MAA_NS::path(value);
#endif
    return std::make_unique<PassiveRecorder>(provider, workspace);
}

} // namespace capture
