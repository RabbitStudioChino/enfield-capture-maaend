#include "NativeFrameStore.h"

#include <cerrno>
#include <charconv>
#include <cmath>
#include <fcntl.h>
#include <stdexcept>
#include <string_view>
#include <system_error>
#include <utility>

#ifdef _WIN32
#include <io.h>
#include <share.h>
#include <sys/stat.h>
#include <windows.h>
#else
#include <unistd.h>
#endif

#include <meojson/json.hpp>

#include <MaaUtils/Platform.h>

namespace capture
{
namespace
{

void assert_real_directory(const std::filesystem::path& directory)
{
    if (!directory.is_absolute()) {
        throw std::invalid_argument("Native capture workspace must be absolute");
    }
    for (auto current = directory; !current.empty(); current = current.parent_path()) {
        if (std::filesystem::is_symlink(std::filesystem::symlink_status(current))) {
            throw std::invalid_argument("Linked native capture directory is forbidden");
        }
#ifdef _WIN32
        const auto attributes = GetFileAttributesW(current.c_str());
        if (attributes != INVALID_FILE_ATTRIBUTES && (attributes & FILE_ATTRIBUTE_REPARSE_POINT)) {
            throw std::invalid_argument("Reparse points in native capture directories are forbidden");
        }
#endif
        if (current == current.root_path()) {
            break;
        }
    }
    if (!std::filesystem::is_directory(directory)) {
        throw std::invalid_argument("Native capture directory does not exist");
    }
}

std::FILE* create_file(const std::filesystem::path& path)
{
#ifdef _WIN32
    int descriptor = -1;
    if (_wsopen_s(&descriptor, path.c_str(), _O_WRONLY | _O_CREAT | _O_EXCL | _O_BINARY, _SH_DENYRW, _S_IREAD | _S_IWRITE) != 0) {
        throw std::runtime_error("Cannot create native capture file exclusively");
    }
    auto* file = _fdopen(descriptor, "wb");
    if (!file) {
        _close(descriptor);
    }
#else
    const int descriptor = ::open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    if (descriptor < 0) {
        throw std::system_error(errno, std::generic_category(), "Cannot create native capture file exclusively");
    }
    auto* file = ::fdopen(descriptor, "wb");
    if (!file) {
        ::close(descriptor);
    }
#endif
    if (!file) {
        throw std::runtime_error("Cannot open native capture file stream");
    }
    return file;
}

void write_synced(std::FILE* file, const void* data, std::size_t bytes)
{
    if (std::fwrite(data, 1, bytes, file) != bytes || std::fflush(file) != 0) {
        throw std::runtime_error("Native capture write or flush failed");
    }
#ifdef _WIN32
    const int result = _commit(_fileno(file));
#else
    const int result = ::fsync(::fileno(file));
#endif
    if (result != 0) {
        throw std::runtime_error("Native capture sync failed");
    }
}

void write_new_file(const std::filesystem::path& path, const void* data, std::size_t bytes)
{
    const std::unique_ptr<std::FILE, decltype(&std::fclose)> file(create_file(path), &std::fclose);
    write_synced(file.get(), data, bytes);
}

void write_json_file(const std::filesystem::path& path, const json::value& value)
{
    const auto text = value.dumps() + "\n";
    auto temporary = path;
    temporary += ".partial";
    write_new_file(temporary, text.data(), text.size());
    if (std::filesystem::exists(path)) {
        throw std::runtime_error("Native capture refuses to overwrite an existing report");
    }
    std::filesystem::rename(temporary, path);
}

json::value number_or_null(double value)
{
    return std::isfinite(value) ? json::value(value) : json::value(nullptr);
}

json::value time_or_null(std::optional<std::int64_t> value)
{
    return value ? json::value(std::to_string(*value)) : json::value(nullptr);
}

json::value position_or_null(const std::optional<mapnavigator::NaviPosition>& position)
{
    if (!position) {
        return nullptr;
    }
    return json::object {
        { "x", number_or_null(position->x) },
        { "y", number_or_null(position->y) },
        { "angle", number_or_null(position->angle) },
        { "score", number_or_null(position->score) },
        { "zone_id", position->zone_id },
        { "camera_angle", position->camera_angle ? number_or_null(*position->camera_angle) : json::value(nullptr) },
        { "floor_y", nullptr },
    };
}

json::value locate_or_null(const std::optional<maplocator::LocateResult>& locate)
{
    if (!locate) {
        return nullptr;
    }
    json::value position;
    if (locate->position) {
        const auto& raw = *locate->position;
        position = json::object {
            { "zone_id", raw.zoneId },
            { "x", number_or_null(raw.x) },
            { "y", number_or_null(raw.y) },
            { "score", number_or_null(raw.score) },
            { "slice_index", raw.sliceIndex },
            { "angle", number_or_null(raw.angle) },
            { "latency_ms", std::to_string(raw.latencyMs) },
        };
    }
    json::value camera;
    if (locate->camRot) {
        camera =
            json::object { { "rot", number_or_null(locate->camRot->rot) }, { "confidence", number_or_null(locate->camRot->confidence) } };
    }
    return json::object {
        { "status", static_cast<int>(locate->status) },
        { "position", position },
        { "message", locate->debugMessage },
        { "character_heading", locate->rot ? number_or_null(*locate->rot) : json::value(nullptr) },
        { "camera_heading", camera },
        { "minimap_absent", locate->minimap_absent },
    };
}

} // namespace

NativeFrameStore::NativeFrameStore(const std::filesystem::path& workspace, CaptureClock clock)
    : clock_(std::move(clock))
{
    assert_real_directory(workspace);
    auto output = workspace.lexically_normal();
    for (const auto* part : { "local", "native-capture" }) {
        output /= part;
        std::filesystem::create_directory(output);
        assert_real_directory(output);
    }
    if (clock_.session_id.empty() || clock_.session_id.find_first_not_of("0123456789abcdef-") != std::string::npos
        || clock_.clock_id.empty()) {
        throw std::invalid_argument("Invalid native capture clock identity");
    }
    run_path_ = output / MAA_NS::path(clock_.session_id);
    if (!std::filesystem::create_directory(run_path_)) {
        throw std::runtime_error("Native capture session directory already exists");
    }
    std::filesystem::create_directory(run_path_ / "images");
    frames_.reset(create_file(run_path_ / "frames.jsonl"));
    write_json_file(
        run_path_ / "session.json",
        json::object {
            { "schema_version", "capture.native.session/1.0" },
            { "session_id", clock_.session_id },
            { "clock_id", clock_.clock_id },
            { "monotonic_anchor_ns", std::to_string(clock_.monotonic_anchor_ns) },
            { "utc_anchor_unix_ns", std::to_string(clock_.utc_anchor_unix_ns) },
            { "purpose", "passive native diagnostics" },
        });
}

void NativeFrameStore::write(const FrameCompletion& completion, const cv::Mat& image)
{
    const std::string prefix = clock_.session_id + "-";
    const std::string_view frame_id(completion.frame_id);
    std::uint64_t sequence = 0;
    const auto suffix = frame_id.substr(std::min(prefix.size(), frame_id.size()));
    const auto parsed = std::from_chars(suffix.data(), suffix.data() + suffix.size(), sequence);
    if (finished_ || completion.clock.session_id != clock_.session_id || completion.clock.clock_id != clock_.clock_id
        || !frame_id.starts_with(prefix) || parsed.ec != std::errc() || parsed.ptr != suffix.data() + suffix.size()
        || sequence <= last_sequence_) {
        throw std::invalid_argument("Native frame identity is foreign, duplicate, or out of order");
    }
    json::value image_record = nullptr;
    if (!image.empty()) {
        const auto relative = MAA_NS::path("images/" + completion.frame_id + ".png");
        const auto target = run_path_ / relative;
        auto temporary = target;
        temporary += ".partial";
        std::vector<unsigned char> encoded;
        if (!cv::imencode(".png", image, encoded)) {
            throw std::runtime_error("Native PNG encoding failed");
        }
        write_new_file(temporary, encoded.data(), encoded.size());
        if (std::filesystem::exists(target)) {
            throw std::runtime_error("Native capture refuses to overwrite an existing image");
        }
        std::filesystem::rename(temporary, target);
        image_record = json::object {
            { "uri", MAA_NS::path_to_utf8_string(relative) },
            { "width", image.cols },
            { "height", image.rows },
            { "channels", image.channels() },
            { "source_format",
              image.channels() == 3   ? "BGR8"
              : image.channels() == 4 ? "BGRA8"
                                      : "GRAY8" },
        };
    }
    const json::value record = json::object {
        { "schema_version", "capture.native.frame/1.0" },
        { "session_id", clock_.session_id },
        { "clock_id", clock_.clock_id },
        { "frame_id", completion.frame_id },
        { "frame_freshness", "unknown" },
        { "timing",
          json::object {
              { "request_monotonic_ns", std::to_string(completion.request_monotonic_ns) },
              { "screencap_completed_monotonic_ns", time_or_null(completion.screencap_completed_monotonic_ns) },
              { "locate_started_monotonic_ns", time_or_null(completion.locate_started_monotonic_ns) },
              { "locate_completed_monotonic_ns", time_or_null(completion.locate_completed_monotonic_ns) },
              { "completed_monotonic_ns", std::to_string(completion.completed_monotonic_ns) },
              { "native_render_timestamp", nullptr },
          } },
        { "image", image_record },
        { "locate_result", locate_or_null(completion.locate_result) },
        { "position_before_normalization", position_or_null(completion.position_before_normalization) },
        { "position_after_normalization", position_or_null(completion.position_after_normalization) },
        { "normalization_id", completion.normalization_id ? json::value(*completion.normalization_id) : json::value(nullptr) },
        { "heading_source", completion.heading_source == mapnavigator::HeadingSource::Camera ? "camera" : "character" },
        { "navigation_valid", completion.navigation_valid },
        { "outcome", completion.outcome },
        { "reason", completion.reason },
    };
    const auto line = record.dumps() + "\n";
    write_synced(frames_.get(), line.data(), line.size());
    last_sequence_ = sequence;
}

void NativeFrameStore::finish(const WriterStats& stats)
{
    if (finished_) {
        return;
    }
    frames_.reset();
    write_json_file(
        run_path_ / "summary.json",
        json::object {
            { "schema_version", "capture.native.summary/1.0" },
            { "status", stats.error.empty() ? "stopped" : "failed" },
            { "accepted", std::to_string(stats.accepted) },
            { "written", std::to_string(stats.written) },
            { "dropped", std::to_string(stats.dropped) },
            { "aborted", std::to_string(stats.aborted) },
            { "peak_frames", std::to_string(stats.peak_frames) },
            { "peak_bytes", std::to_string(stats.peak_bytes) },
            { "last_dropped_frame_id", stats.last_dropped_frame_id },
            { "last_drop_reason", stats.last_drop_reason },
            { "error", stats.error },
        });
    finished_ = true;
}

const std::filesystem::path& NativeFrameStore::runPath() const
{
    return run_path_;
}

} // namespace capture
