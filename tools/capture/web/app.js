import {Camera} from "/shared/camera.js";
import {canvasPoint, wheelZoomFactor} from "/shared/map_view.js";
import {parseNmsh} from "/shared/navmesh_3d_data.js";

const $ = (id) => document.getElementById(id);
const canvas = $("map-canvas");
const context = canvas.getContext("2d");
const camera = new Camera();
const parameterInputs = {radius: "radius", spacing: "spacing", views: "views", max_points: "max-points"};
const exclusiveControls =
  "#capture-form input, #capture-form select, #capture-form button, .toolbar input, .toolbar select, .toolbar button, #saved-selections button, #export, #load-task";
const state = {
  catalog: null,
  region: null,
  map: null,
  image: null,
  mesh: null,
  polygon: [],
  drawing: false,
  plan: null,
  zones: [],
  zonesLoaded: false,
  selections: [],
  selectionsLoading: true,
  task: "",
  checkpoint: null,
  cursor: "",
  events: [],
  sessions: new Set(),
  mapGeneration: 0,
};
let token = "";
let drag = null;
let busy = false;
let polling = false;

function message(text, error = false) {
  $("message").textContent = text;
  $("message").classList.toggle("error", error);
}

function selectionMessage(text, error = false) {
  $("selections-status").textContent = text;
  $("selections-status").classList.toggle("error", error);
}

function renderSelections() {
  $("saved-selections").replaceChildren();
  for (const selection of state.selections) {
    const row = document.createElement("div");
    row.className = "saved-selection";
    const load = document.createElement("button");
    load.type = "button";
    load.textContent = `${selection.name} · ${selection.polygon.length} 个顶点 · ${selection.created_utc.slice(0, 10)}`;
    load.title = `底图 ${selection.map} · 锚点 ${selection.region} · 点击加载范围与采样参数`;
    load.disabled = busy || !state.catalog;
    load.addEventListener(
      "click",
      () =>
        void operation(
          async () => {
            if (!state.catalog) throw new Error("区域目录尚未读取完成，请稍后加载。");
            const {selection: saved} = await api("/api/selections/load", {id: selection.id});
            const region = state.catalog.regions.find((item) => item.id === saved.region && item.map === saved.map);
            if (!region) throw new Error("选区锚点或底图已失效，请重新打开页面读取当前目录。");
            await chooseRegion(region, false);
            state.polygon = saved.polygon;
            for (const [
              key,
              id,
            ] of Object.entries(parameterInputs))
              $(id).value = saved[key];
            $("floor").value = saved.floor_y ?? "";
            updateTiers(saved.tier || "");
            $("selection-name").value = saved.name;
            invalidate();
            fit();
            const notice = tierNotice();
            selectionMessage(`已加载“${saved.name}”，可预览或开始采集。${notice}`, state.zonesLoaded && !!notice);
          },
          true,
          selectionMessage,
        ),
    );
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "删除";
    remove.setAttribute("aria-label", `删除选区 ${selection.name}`);
    remove.disabled = busy || !state.catalog;
    remove.addEventListener(
      "click",
      () =>
        void operation(
          async () => {
            const result = await api("/api/selections/delete", {id: selection.id});
            state.selections = result.selections;
            renderSelections();
            selectionMessage(`已删除“${selection.name}”。`);
          },
          true,
          selectionMessage,
        ),
    );
    row.append(load, remove);
    $("saved-selections").append(row);
  }
}

async function loadSelections() {
  try {
    const result = await api("/api/selections");
    state.selections = result.selections;
    renderSelections();
    selectionMessage(
      state.selections.length ? "点击选区名称即可加载，不会启动任务。" : "尚未保存选区。完成圈选后输入名称并保存。",
    );
  } catch (error) {
    selectionMessage(error.message, true);
  } finally {
    state.selectionsLoading = false;
  }
}

async function api(path, body) {
  const response = await fetch(
    path,
    body === undefined
      ? {cache: "no-store"}
      : {
          method: "POST",
          headers: {"Content-Type": "application/json", "X-Capture-Token": token},
          body: JSON.stringify(body),
        },
  );
  const value = await response.json();
  if (!response.ok || value.ok === false) throw new Error(value.error || `请求失败 ${response.status}`);
  return value;
}

function invalidate() {
  state.plan = null;
  $("plan-info").textContent = "范围或参数已更新。可预览，也可直接开始采集。";
  render();
}

function fit() {
  if (!state.map) return;
  const polygon = state.polygon;
  const center = state.region?.anchor_position;
  const radius = Number($("radius").value) || 60;
  let bounds = [
    0,
    0,
    ...state.map.size,
  ];
  if (polygon.length)
    bounds = [
      Math.min(...polygon.map((p) => p[0])),
      Math.min(...polygon.map((p) => p[1])),
      Math.max(...polygon.map((p) => p[0])),
      Math.max(...polygon.map((p) => p[1])),
    ];
  else if (center)
    bounds = [
      center[0] - radius * 1.5,
      center[1] - radius * 1.5,
      center[0] + radius * 1.5,
      center[1] + radius * 1.5,
    ];
  camera.fitView(bounds, canvas.clientWidth, canvas.clientHeight, 40);
  render();
}

function tierNotice() {
  if (!$("tier").value) return "";
  if (!state.zonesLoaded) return "导航层级尚未核对，已保留所选层级。";
  return state.zones.some(
    (zone) => zone.is_tier && zone.geometry_zone_id === state.map?.zone_id && zone.name === $("tier").value,
  )
    ? ""
    : "所选层级已不在当前底图的导航目录中，请重新选择层级后再预览或开始。";
}

function updateTiers(selected = $("tier").value) {
  $("tier").replaceChildren(new Option("自动选择可达层", ""));
  for (const zone of state.zones.filter((z) => z.is_tier && z.geometry_zone_id === state.map?.zone_id)) {
    $("tier").add(new Option(zone.name, zone.name));
  }
  if (selected && ![...$("tier").options].some((option) => option.value === selected))
    $("tier").add(new Option(`${selected}（${state.zonesLoaded ? "当前底图不可用" : "待核对"}）`, selected));
  $("tier").value = selected;
}

async function loadMesh(generation) {
  try {
    const response = await fetch(`/api/mesh?map=${encodeURIComponent(state.map.id)}`);
    if (!response.ok) throw new Error((await response.json()).error || "网格加载失败");
    const mesh = parseNmsh(await response.arrayBuffer());
    if (generation !== state.mapGeneration) return;
    state.mesh = mesh;
    render();
  } catch (error) {
    if (generation === state.mapGeneration) message(error.message, true);
  }
}

async function setMap(mapId, fetchMesh = true) {
  const frame = state.catalog.maps.find((item) => item.id === mapId);
  if (!frame) return;
  if (state.map?.id === mapId) {
    updateTiers();
    fit();
    return;
  }
  state.map = frame;
  state.image = null;
  state.mesh = null;
  const generation = ++state.mapGeneration;
  $("map").value = mapId;
  $("map-info").textContent = `${frame.name} · ${frame.zone} · ${frame.size.join(" × ")}`;
  updateTiers("");
  fit();
  const image = new Image();
  image.onload = () => {
    if (generation === state.mapGeneration) {
      state.image = image;
      render();
    }
  };
  image.onerror = () => {
    if (generation === state.mapGeneration) message("底图加载失败，请检查正式地图资源是否齐全。", true);
  };
  image.src = `/api/map?map=${encodeURIComponent(mapId)}`;
  if (fetchMesh && $("show-mesh").checked) void loadMesh(generation);
}

async function chooseRegion(region, fetchMesh = true) {
  state.region = region;
  state.polygon = [];
  state.drawing = false;
  $("draw").classList.remove("active");
  canvas.classList.remove("drawing");
  $("region").value = region.name;
  $("matches").replaceChildren();
  $("region-info").textContent =
    `${region.name} · ${region.level_name} · 锚点 ${region.id}。默认采集锚点周边，可调半径或圈选。`;
  invalidate();
  await setMap(region.map, fetchMesh);
}

function search() {
  const needle = $("region").value.trim().toLocaleLowerCase();
  state.region = null;
  state.polygon = [];
  state.drawing = false;
  $("draw").classList.remove("active");
  canvas.classList.remove("drawing");
  invalidate();
  $("matches").replaceChildren();
  if (!needle || !state.catalog) return;
  const matches = state.catalog.regions.filter((item) =>
    [
      item.id,
      ...Object.values(item.names),
    ].some((name) => name.toLocaleLowerCase().includes(needle)),
  );
  for (const region of matches.slice(0, 12)) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${region.name} · ${region.level_name} · ${region.id}`;
    button.addEventListener("click", () => void chooseRegion(region));
    $("matches").append(button);
  }
  $("region-info").textContent = matches.length
    ? `找到 ${matches.length} 个区域。完整唯一名称可直接开始，重名请选择具体标记。`
    : "当前目录未匹配该名称。";
}

function request() {
  if (state.drawing) throw new Error("请先点击完成圈选，或使用周边半径。");
  const body = {
    region: state.region?.id || $("region").value.trim(),
  };
  for (const [
    key,
    id,
  ] of Object.entries(parameterInputs))
    body[key] = Number($(id).value);
  if (state.polygon.length) body.polygon = state.polygon;
  if ($("tier").value) body.tier = $("tier").value;
  if ($("floor").value !== "") body.floor_y = Number($("floor").value);
  return body;
}

async function operation(callback, exclusive = true, feedback = message) {
  if (busy && exclusive) return;
  if (exclusive) {
    busy = true;
    for (const element of document.querySelectorAll(exclusiveControls)) element.disabled = true;
  }
  try {
    await callback();
  } catch (error) {
    feedback(error.message, true);
  } finally {
    if (exclusive) {
      busy = false;
      for (const element of document.querySelectorAll(exclusiveControls)) element.disabled = false;
    }
    updateTaskControls();
  }
}

async function showPlan(plan) {
  if (state.region?.id !== plan.region.id || state.map?.id !== plan.region.map) await chooseRegion(plan.region);
  state.polygon = plan.selection.scope === "drawn_polygon" ? plan.selection.polygon : [];
  state.plan = plan;
  $("plan-info").textContent =
    `${plan.region.name} · 可达点 ${plan.coverage.point_count} · 摄影视角 ${plan.waypoints.length} · 排除 ${plan.coverage.rejected}。资源指纹已固定。`;
  render();
}

function selectTask(taskId) {
  if (state.task === taskId) return;
  state.task = taskId;
  state.checkpoint = null;
  state.cursor = "";
  state.events = [];
  state.sessions.clear();
  $("task").value = taskId;
  $("session").replaceChildren(new Option("最新会话", ""));
  $("events").textContent = "正在读取任务事件…";
  $("task-state").textContent = "正在读取任务状态…";
  $("progress-bar").style.width = "0%";
  $("identity").textContent = "";
  updateTaskControls();
}

function updateTaskControls() {
  const checkpoint = state.checkpoint;
  const ready = checkpoint?.checkpoint_ready && !checkpoint.launch_pending;
  for (const button of document.querySelectorAll("[data-action]")) {
    button.disabled =
      !state.task || !checkpoint || (button.dataset.action !== "stop" && !ready) || checkpoint.status === "stopping";
  }
  $("export").disabled = busy || !ready || !checkpoint.session_id;
  $("session").disabled = !ready || !checkpoint.session_id;
}

function launchDiagnostics(checkpoint) {
  return [
    checkpoint.launch_log ? `启动日志 ${checkpoint.launch_log}` : "",
    checkpoint.launch_created_utc ? `启动时间 ${checkpoint.launch_created_utc}` : "",
    checkpoint.launcher_exit_code !== undefined && checkpoint.launcher_exit_code !== null
      ? `启动进程退出码 ${checkpoint.launcher_exit_code}`
      : "",
    checkpoint.launch_log_error ? `启动日志读取失败 ${checkpoint.launch_log_error}` : "",
    checkpoint.launch_error ? `启动失败 ${checkpoint.launch_error}` : "",
    checkpoint.launch_log_tail || "",
  ]
    .filter(Boolean)
    .join("\n");
}

async function refresh() {
  if (!state.task || polling) return;
  polling = true;
  const taskId = state.task;
  try {
    const checkpoint = await api(`/api/status?task=${encodeURIComponent(taskId)}`);
    if (state.task !== taskId) return;
    state.checkpoint = checkpoint;
    updateTaskControls();
    const initializing = !checkpoint.checkpoint_ready || checkpoint.launch_pending;
    const launchFailed =
      checkpoint.status === "created" &&
      !checkpoint.active &&
      checkpoint.launcher_exit_code !== undefined &&
      checkpoint.launcher_exit_code !== null;
    const labels = {
      starting: "正在初始化：核对资源并创建检查点",
      created: launchFailed
        ? "初始化进程已退出，检查点仍停留在创建阶段，请查看启动日志"
        : "任务已创建，正在准备采集进程",
      stopping: "正在请求停止，尚未确认退出",
      launch_unknown: "原启动进程状态未知",
      failed: "任务失败",
      stopped: "已停止",
      paused: "已暂停",
    };
    const progress = initializing
      ? `计划 ${checkpoint.count} 个视角，尚无采集进度`
      : `${checkpoint.next_index}/${checkpoint.count}`;
    const process =
      checkpoint.status === "launch_unknown" || (checkpoint.launch_pending && !checkpoint.launcher_tracked)
        ? "原启动进程状态未知，请核对日志"
        : checkpoint.launch_pending
          ? "启动进程准备中"
          : checkpoint.active
            ? "进程运行中"
            : "进程未运行";
    $("task-state").textContent =
      `${labels[checkpoint.status] || checkpoint.status} · ${progress} · ${process}${checkpoint.error ? ` · 原因：${checkpoint.error}` : ""}${checkpoint.status === "paused" ? "。请排除上述原因，保持游戏窗口可见且未最小化；点击“恢复”后，请在五秒内切回游戏并保持前台。" : ""}`;
    $("task-state").classList.toggle("error", !!checkpoint.error || launchFailed);
    $("progress-bar").style.width = `${checkpoint.count ? (100 * checkpoint.next_index) / checkpoint.count : 0}%`;
    $("identity").textContent =
      `会话 ${checkpoint.session_id || "待创建"}\ngeneration ${checkpoint.generation || "待创建"}`;
    if (checkpoint.session_id && !state.sessions.has(checkpoint.session_id)) {
      state.sessions.add(checkpoint.session_id);
      $("session").add(new Option(checkpoint.session_id, checkpoint.session_id));
    }
    if (!checkpoint.checkpoint_ready) {
      $("events").textContent = `尚未创建任务检查点，事件将在初始化后自动读取。\n\n${launchDiagnostics(checkpoint)}`;
      return;
    }
    const result = await api(
      `/api/events?task=${encodeURIComponent(taskId)}&after=${encodeURIComponent(state.cursor)}`,
    );
    if (state.task !== taskId) return;
    state.cursor = result.cursor;
    state.events.push(...result.events);
    state.events = state.events.slice(-80);
    $("events").textContent = [
      state.events.map((item) => JSON.stringify(item)).join("\n") || "等待首个事件",
      launchDiagnostics(checkpoint),
    ].join("\n\n");
    $("events").scrollTop = $("events").scrollHeight;
  } catch (error) {
    if (state.task === taskId) {
      state.checkpoint = null;
      updateTaskControls();
      $("task-state").textContent = "任务状态暂不可读，请查看错误与本地启动日志。";
      message(`任务状态暂不可读 ${error.message}。保留本地启动日志。`, true);
    }
  } finally {
    polling = false;
  }
}

function render() {
  const width = canvas.clientWidth,
    height = canvas.clientHeight;
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
  }
  context.setTransform(dpr, 0, 0, dpr, 0, 0);
  context.clearRect(0, 0, width, height);
  context.save();
  context.translate(camera.offsetX, camera.offsetY);
  context.scale(camera.viewScale, camera.viewScale);
  if (state.image && state.map) context.drawImage(state.image, 0, 0, ...state.map.size);
  const stroke = (points, color, fill = false, closed = false) => {
    if (!points.length) return;
    context.beginPath();
    context.moveTo(...points[0]);
    for (const point of points.slice(1)) context.lineTo(...point);
    if (closed) context.closePath();
    context.strokeStyle = color;
    context.lineWidth = 2 / camera.viewScale;
    context.stroke();
    if (fill) {
      context.fillStyle = "#56c9bb24";
      context.fill();
    }
  };
  if ($("show-mesh").checked && state.mesh) {
    const {indices, vertices} = state.mesh;
    const step = Math.max(1, Math.ceil(indices.length / 36000));
    context.beginPath();
    for (let index = 0; index < indices.length; index += step * 3) {
      const triangle = [
        0,
        1,
        2,
      ].map((corner) => [
        vertices[indices[index + corner] * 3],
        vertices[indices[index + corner] * 3 + 1],
      ]);
      context.moveTo(...triangle[0]);
      context.lineTo(...triangle[1]);
      context.lineTo(...triangle[2]);
      context.closePath();
    }
    context.strokeStyle = "#8dbcd366";
    context.lineWidth = 0.7 / camera.viewScale;
    context.stroke();
  }
  if (state.polygon.length) stroke(state.polygon, "#66e0d1", true, !state.drawing);
  else if (state.region) {
    context.beginPath();
    context.arc(...state.region.anchor_position, Number($("radius").value) || 60, 0, Math.PI * 2);
    context.strokeStyle = "#66e0d1";
    context.lineWidth = 2 / camera.viewScale;
    context.fillStyle = "#56c9bb24";
    context.fill();
    context.stroke();
  }
  if (state.plan) {
    stroke(state.plan.coverage.route, "#f4cc7a");
    context.fillStyle = "#ffda8e";
    for (const point of state.plan.waypoints.filter((item) => item.view_index === 0)) {
      context.beginPath();
      context.arc(...point.position_xy, 3 / camera.viewScale, 0, Math.PI * 2);
      context.fill();
    }
  }
  context.restore();
  if (!state.catalog || !state.map) return;
  for (const region of state.catalog.regions.filter((item) => item.map === state.map.id)) {
    const [
      x,
      y,
    ] = camera.worldToCanvas(...region.anchor_position);
    if (x < -80 || y < -20 || x > width + 80 || y > height + 20) continue;
    context.beginPath();
    context.arc(x, y, region.id === state.region?.id ? 7 : 5, 0, Math.PI * 2);
    context.fillStyle = region.id === state.region?.id ? "#69f4d5" : "#d6e4ea";
    context.fill();
    context.font = "12px system-ui";
    context.lineWidth = 3;
    context.strokeStyle = "#0c1923";
    context.strokeText(region.name, x + 10, y + 4);
    context.fillStyle = "#f2f5ef";
    context.fillText(region.name, x + 10, y + 4);
  }
}

canvas.addEventListener(
  "wheel",
  (event) => {
    event.preventDefault();
    camera.zoomAt(...canvasPoint(event, canvas), wheelZoomFactor(event, canvas.clientHeight));
    render();
  },
  {passive: false},
);
canvas.addEventListener("pointerdown", (event) => {
  if (event.button !== 0) return;
  const point = canvasPoint(event, canvas);
  drag = {start: point, last: point, moved: false};
  canvas.setPointerCapture(event.pointerId);
});
canvas.addEventListener("pointermove", (event) => {
  if (!drag) return;
  const point = canvasPoint(event, canvas);
  if (Math.hypot(point[0] - drag.start[0], point[1] - drag.start[1]) > 4) drag.moved = true;
  if (drag.moved) {
    camera.panBy(point[0] - drag.last[0], point[1] - drag.last[1]);
    render();
  }
  drag.last = point;
});
canvas.addEventListener("pointerup", (event) => {
  if (!drag) return;
  const moved = drag.moved;
  drag = null;
  if (moved || !state.map || busy) return;
  const point = canvasPoint(event, canvas);
  if (state.drawing) {
    const world = camera.canvasToWorld(...point);
    if (world[0] < 0 || world[1] < 0 || world[0] > state.map.size[0] || world[1] > state.map.size[1]) return;
    if (state.polygon.length >= 64) return message("选区最多 64 个顶点。", true);
    state.polygon.push(world);
    invalidate();
    return;
  }
  const region = state.catalog.regions
    .filter((item) => item.map === state.map.id)
    .map((item) => ({item, point: camera.worldToCanvas(...item.anchor_position)}))
    .find((hit) => Math.hypot(hit.point[0] - point[0], hit.point[1] - point[1]) < 16);
  if (region) void chooseRegion(region.item);
});
canvas.addEventListener("pointercancel", () => {
  drag = null;
});
$("draw").addEventListener("click", () => {
  if (busy) return;
  if (!state.region) return message("请先选择一个真实区域锚点，再圈选采集范围。", true);
  state.polygon = [];
  state.drawing = true;
  invalidate();
  $("draw").classList.add("active");
  canvas.classList.add("drawing");
  message("在地图上逐个点击落点，至少三个点后点击完成圈选。坐标使用当前底图像素。");
});
$("finish").addEventListener("click", () => {
  if (state.polygon.length < 3) return message("圈选范围至少需要三个顶点。", true);
  state.drawing = false;
  $("draw").classList.remove("active");
  canvas.classList.remove("drawing");
  invalidate();
});
$("clear").addEventListener("click", () => {
  state.polygon = [];
  state.drawing = false;
  $("draw").classList.remove("active");
  canvas.classList.remove("drawing");
  invalidate();
});
$("save-selection").addEventListener(
  "click",
  () =>
    void operation(
      async () => {
        if (state.selectionsLoading) throw new Error("本地选区列表正在读取，请稍后保存。");
        if (!state.region || state.region.map !== state.map?.id) throw new Error("请先选择真实区域锚点。");
        const body = request();
        if (!body.polygon || body.polygon.length < 3) throw new Error("请先完成至少三个顶点的圈选，再保存选区。");
        const name = $("selection-name").value.trim();
        if (!name) throw new Error("请输入选区名称。");
        selectionMessage("正在保存到本地选区列表…");
        const result = await api("/api/selections", {...body, name, map: state.map.id});
        state.selections = result.selections;
        renderSelections();
        selectionMessage(`已保存“${result.selection.name}”。下次打开本工作区时会自动读取列表。`);
      },
      true,
      selectionMessage,
    ),
);
$("fit").addEventListener("click", fit);
$("region").addEventListener("input", search);
$("map").addEventListener("change", () => {
  state.region = null;
  state.polygon = [];
  invalidate();
  void setMap($("map").value);
});
$("show-mesh").addEventListener("change", () => {
  if ($("show-mesh").checked && !state.mesh) void loadMesh(state.mapGeneration);
  render();
});
for (const id of [
  "radius",
  "spacing",
  "views",
  "max-points",
  "tier",
  "floor",
])
  $(id).addEventListener("input", invalidate);
$("preview").addEventListener(
  "click",
  () =>
    void operation(async () => {
      message("正在规划真实可达采样点…");
      await showPlan(await api("/api/preview", request()));
      message("采样点与路线已显示，可直接开始采集。");
    }),
);
$("capture-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void operation(async () => {
    message("正在规划并启动任务…");
    const result = await api("/api/start", request());
    selectTask(result.task_id);
    await showPlan(result.plan);
    message(`${result.message}\n启动日志 ${result.launch_log}`);
    await refresh();
  });
});
$("load-task").addEventListener(
  "click",
  () =>
    void operation(async () => {
      selectTask($("task").value.trim());
      await refresh();
    }),
);
for (const button of document.querySelectorAll("[data-action]"))
  button.addEventListener(
    "click",
    () =>
      void operation(async () => {
        if (!state.task) throw new Error("请先启动或载入任务。");
        const action = button.dataset.action;
        if (action !== "stop" && (!state.checkpoint?.checkpoint_ready || state.checkpoint.launch_pending))
          throw new Error("任务仍在初始化，暂不能暂停或恢复。");
        const result = await api("/api/control", {
          task: state.task,
          action,
          command_id: `${action}-${crypto.randomUUID()}`,
          generation: state.checkpoint?.generation,
        });
        message(
          result.result === "cancellation_requested"
            ? "已记录停止请求，正在等待启动进程确认取消；请查看任务状态和启动日志。"
            : `${action} ${result.result || "已提交"}${action === "resume" ? "，请在五秒内切回游戏并保持前台。" : ""}`,
        );
        await refresh();
      }, false),
  );
$("export").addEventListener(
  "click",
  () =>
    void operation(async () => {
      if (!state.task) throw new Error("请先载入任务。");
      if (!state.checkpoint?.checkpoint_ready || !state.checkpoint.session_id)
        throw new Error("任务仍在初始化，暂不能导出。");
      const result = await api("/api/export", {task: state.task, session: $("session").value || null});
      $("export-result").textContent = result.bundle;
      message("冻结包已导出并通过格式与文件完整性校验，图像几何质量待审。");
    }),
);
new ResizeObserver(render).observe(canvas);
updateTaskControls();

async function initialize() {
  void loadSelections();
  const config = await api("/api/config");
  token = config.token;
  $("workspace").textContent = `工作区 ${config.workspace} · 所有采集数据、计划与日志保存在 local/`;
  state.catalog = await api("/api/catalog");
  for (const frame of state.catalog.maps) $("map").add(new Option(frame.name, frame.id));
  const initial = state.catalog.regions.filter((region) => region.name === $("region").value);
  if (initial.length === 1) await chooseRegion(initial[0]);
  else {
    await setMap(state.catalog.maps[0].id);
    search();
  }
  renderSelections();
  message("选择区域后可以直接开始。自动任务将控制游戏移动和镜头，开始或恢复后请在五秒内切回游戏并保持前台。");
  try {
    const result = await api("/api/zones");
    state.zones = result.zones;
    state.zonesLoaded = true;
    updateTiers();
    if ($("tier").value) {
      const notice = tierNotice();
      selectionMessage(notice || "所选导航层级已核对，可以预览或开始采集。", !!notice);
    }
  } catch (error) {
    message(`地图已加载，导航资源尚未就绪 ${error.message}`, true);
  }
}

void initialize().catch((error) => message(error.message, true));
setInterval(() => void refresh(), 1500);
