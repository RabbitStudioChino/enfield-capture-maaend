"""Read COLMAP text models and keep SfM and canonical-world coordinates explicit."""

import math
from pathlib import Path

from tools.capture.validate import require

IDENTITY = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
CAMERA_PARAMETERS = {
    "SIMPLE_PINHOLE": 3,
    "PINHOLE": 4,
    "SIMPLE_RADIAL": 4,
    "RADIAL": 5,
    "OPENCV": 8,
    "OPENCV_FISHEYE": 8,
    "FULL_OPENCV": 12,
    "FOV": 5,
    "SIMPLE_RADIAL_FISHEYE": 4,
    "RADIAL_FISHEYE": 5,
    "THIN_PRISM_FISHEYE": 12,
}


def numbers(values):
    result = [float(value) for value in values]
    require(all(math.isfinite(value) for value in result), "non-finite model number")
    return result


def transpose(matrix):
    return [list(row) for row in zip(*matrix)]


def multiply(left, right):
    return [[sum(a * b for a, b in zip(row, column)) for column in zip(*right)] for row in left]


def apply(matrix, vector):
    return [sum(a * b for a, b in zip(row, vector)) for row in matrix]


def rotation(quaternion):
    w, x, y, z = numbers(quaternion)
    require(abs(w * w + x * x + y * y + z * z - 1) < 1e-4, "COLMAP quaternion is not unit length")
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]


def validate_transform(transform):
    if transform is None:
        return
    require(set(transform) == {"s", "R", "t", "coordinate_revision", "scale_evidence"}, "invalid world transform keys")
    require(
        isinstance(transform["coordinate_revision"], str) and transform["coordinate_revision"],
        "missing coordinate revision",
    )
    require(isinstance(transform["scale_evidence"], str) and transform["scale_evidence"], "missing scale evidence")
    require(
        type(transform["s"]) in (int, float) and math.isfinite(transform["s"]) and transform["s"] > 0,
        "invalid positive scale",
    )
    matrix = transform["R"]
    require(
        isinstance(matrix, list)
        and len(matrix) == 3
        and all(isinstance(row, list) and len(row) == 3 for row in matrix),
        "R must be 3x3",
    )
    for row in matrix:
        numbers(row)
    require(isinstance(transform["t"], list) and len(transform["t"]) == 3, "t must have three values")
    numbers(transform["t"])
    product = multiply(matrix, transpose(matrix))
    require(all(abs(product[i][j] - IDENTITY[i][j]) < 1e-6 for i in range(3) for j in range(3)), "R is not orthonormal")
    a, b, c = matrix
    determinant = (
        a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0]) + a[2] * (b[0] * c[1] - b[1] * c[0])
    )
    require(abs(determinant - 1) < 1e-6, "R must be a proper rotation")


def data_lines(path, keep_empty=False):
    return [
        line.strip()
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#") and (keep_empty or line.strip())
    ]


def read_model(root):
    """Validate IDs, observations and reciprocal tracks in the documented text format."""
    root = Path(root)
    cameras, images, points = {}, {}, {}
    for line in data_lines(root / "cameras.txt"):
        values = line.split()
        require(len(values) >= 5, "malformed COLMAP camera")
        camera_id, width, height = int(values[0]), int(values[2]), int(values[3])
        model = values[1]
        params = numbers(values[4:])
        require(camera_id >= 0 and camera_id not in cameras and width > 0 and height > 0, "invalid or duplicate camera")
        require(
            model in CAMERA_PARAMETERS and len(params) == CAMERA_PARAMETERS[model], f"unsupported camera model {model}"
        )
        require(params[0] > 0 and (model != "PINHOLE" or params[1] > 0), "invalid focal length")
        cameras[camera_id] = {
            "camera_id": camera_id,
            "model": model,
            "width": width,
            "height": height,
            "params": params,
        }
    lines = data_lines(root / "images.txt", keep_empty=True)
    require(len(lines) % 2 == 0, "images.txt must have two lines per image")
    names = set()
    for index in range(0, len(lines), 2):
        values = lines[index].split(maxsplit=9)
        require(len(values) == 10, "malformed COLMAP image")
        image_id, camera_id = int(values[0]), int(values[8])
        name = values[9]
        require(
            image_id >= 0 and image_id not in images and camera_id in cameras and name not in names,
            "invalid image identity",
        )
        names.add(name)
        observations = lines[index + 1].split()
        require(len(observations) % 3 == 0, "malformed 2D observations")
        xy = [numbers(observations[start : start + 2]) for start in range(0, len(observations), 3)]
        point_ids = [int(observations[start + 2]) for start in range(0, len(observations), 3)]
        require(all(point_id >= -1 for point_id in point_ids), "invalid point3D ID")
        images[image_id] = {
            "image_id": image_id,
            "camera_id": camera_id,
            "name": name,
            "R_camera_from_sfm": rotation(values[1:5]),
            "t_camera_from_sfm": numbers(values[5:8]),
            "xy": xy,
            "point_ids": point_ids,
        }
    for line in data_lines(root / "points3D.txt"):
        values = line.split()
        require(len(values) >= 10 and (len(values) - 8) % 2 == 0, "malformed COLMAP point track")
        point_id = int(values[0])
        rgb = [int(value) for value in values[4:7]]
        error = numbers([values[7]])[0]
        track = [(int(values[start]), int(values[start + 1])) for start in range(8, len(values), 2)]
        require(
            point_id >= 0 and point_id not in points and all(0 <= value <= 255 for value in rgb) and error >= 0,
            "invalid point",
        )
        require(len(track) >= 2 and len(set(track)) == len(track), "invalid point track")
        for image_id, point_index in track:
            require(
                image_id in images
                and 0 <= point_index < len(images[image_id]["point_ids"])
                and images[image_id]["point_ids"][point_index] == point_id,
                "point track is not reciprocal",
            )
        points[point_id] = {
            "point_id": point_id,
            "xyz": numbers(values[1:4]),
            "rgb": rgb,
            "error_px": error,
            "track": track,
        }
    for image in images.values():
        for index, point_id in enumerate(image["point_ids"]):
            require(
                point_id == -1 or (point_id in points and (image["image_id"], index) in points[point_id]["track"]),
                "observation is not reciprocal",
            )
    require(bool(cameras) and bool(images) and bool(points), "empty COLMAP reconstruction")
    return {"cameras": cameras, "images": images, "points": points}


def camera_records(model, transform=None):
    validate_transform(transform)
    alignment = transform["R"] if transform else IDENTITY
    scale = transform["s"] if transform else 1
    offset = transform["t"] if transform else [0, 0, 0]
    records = []
    for image in model["images"].values():
        inverse = transpose(image["R_camera_from_sfm"])
        center = [-value for value in apply(inverse, image["t_camera_from_sfm"])]
        position = [scale * a + b for a, b in zip(apply(alignment, center), offset)]
        records.append(
            {
                "image_id": image["image_id"],
                "image_name": image["name"],
                "camera_id": image["camera_id"],
                "center_sfm": center,
                "position": position,
                "R_frame_from_web_camera": multiply(multiply(alignment, inverse), [[1, 0, 0], [0, -1, 0], [0, 0, -1]]),
            }
        )
    return {
        "coordinate_system": "world" if transform else "sfm",
        "intrinsics": list(model["cameras"].values()),
        "images": records,
    }


def quality_statistics(model, total_images):
    require(total_images >= len(model["images"]), "registered images exceed prepared images")
    neighbors = {key: set() for key in model["images"]}
    for point in model["points"].values():
        ids = {image for image, _ in point["track"]}
        for image in ids:
            neighbors[image].update(ids - {image})
    unseen, components = set(neighbors), []
    while unseen:
        pending, count = [unseen.pop()], 0
        while pending:
            image = pending.pop()
            count += 1
            next_ids = neighbors[image] & unseen
            unseen.difference_update(next_ids)
            pending.extend(next_ids)
        components.append(count)
    errors = sorted(point["error_px"] for point in model["points"].values())
    tracks = [len(point["track"]) for point in model["points"].values()]
    return {
        "input_images": total_images,
        "registered_images": len(model["images"]),
        "registration_ratio": len(model["images"]) / total_images,
        "sparse_points": len(errors),
        "connected_components": sorted(components, reverse=True),
        "mean_track_length": sum(tracks) / len(tracks),
        "reprojection_error_mean_px": sum(errors) / len(errors),
        "reprojection_error_p95_px": errors[min(len(errors) - 1, math.ceil(len(errors) * 0.95) - 1)],
        "reprojection_source": "COLMAP stored error after bundle adjustment",
        "status": "needs_review",
        "manual_checks": [
            "held-out views",
            "holes and dynamic-content leakage",
            "walkable surfaces",
            "scale and axis alignment",
        ],
        "threshold_status": "not_calibrated",
        "geometry_acceptance": "not_assessed",
    }
